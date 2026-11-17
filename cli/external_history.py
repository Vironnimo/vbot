"""Thin RPC client for external conversation reads and imports."""

from __future__ import annotations

import argparse
import json
from typing import Any

from cli._input import _read_text_file
from cli._recovery import format_command, target_options
from cli.formatting import inline_text
from cli.rpc_client import rpc_call
from cli.server_management import CommandResult, ServerInstance


def dispatch_external(args: argparse.Namespace, instance: ServerInstance) -> CommandResult:
    action = args.command
    method = {"import-session": "external.import_session", "import-memory": "memory.import"}.get(
        action, "external." + action
    )
    params: dict[str, Any] = {}
    for name, target in (
        ("source", "source"),
        ("path", "path"),
        ("session", "source_session_id"),
        ("limit", "limit"),
        ("offset", "offset"),
        ("query", "query"),
        ("char_offset", "char_offset"),
        ("agent", "agent_id"),
        ("project", "project_id"),
        ("scope", "scope"),
        ("expected_digest", "expected_digest"),
        ("working_project", "working_project_id"),
    ):
        if getattr(args, name, None) is not None:
            params[target] = getattr(args, name)
    if action == "import-memory" and getattr(args, "entries_file", None):
        from core.external_history.history import memory_entries

        try:
            params["entries"] = memory_entries(_read_text_file(args.entries_file))
        except (OSError, UnicodeError, ValueError) as exc:
            message = f"Cannot read reviewed entries: {exc}. Nothing was imported."
            return CommandResult(
                ok=False,
                message=json.dumps(
                    {"ok": False, "error": {"code": "invalid_entries_file", "message": message}}
                )
                if args.json
                else message,
                instance=instance,
            )
    response = rpc_call(instance, method, params)
    if not response.ok:
        if args.json:
            return CommandResult(
                ok=False,
                message=json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": response.failure.code if response.failure else "request_failed",
                            "message": response.message,
                        },
                    },
                    ensure_ascii=True,
                ),
                instance=instance,
                failure=response.failure,
            )
        return response.to_command_result()
    data = response.data
    if args.json:
        return CommandResult(
            ok=True, message=json.dumps(data, ensure_ascii=True), instance=instance
        )
    if action == "import-session":
        lines = [
            f"Imported {data['entry_count']} entries from {inline_text(data['source'])}.",
            f"agent: {inline_text(data['agent_id'])}",
            f"session: {inline_text(data['session_id'])}",
        ]
    elif action == "import-memory":
        lines = [
            f"{len(data['entries'])} reviewed Memory entries are present in the destination.",
            *(f"  {entry['id']}: {inline_text(entry['content'])}" for entry in data["entries"]),
        ]
    else:
        items = data.get("items", [])
        lines = [f"{action}: {len(items)} of {data.get('total', len(items))} items."]
        if "path" in data:
            lines.append(f"source_path: {inline_text(data['path'])}")
        for item in items:
            if action in {"sources", "list"}:
                title = inline_text(item.get("title", item.get("name", "")))
                lines.append(f"  {inline_text(item['id'])}  {title}")
                lines.append(f"    path: {inline_text(item['path'])}")
            else:
                label = f"[{item['ordinal']}] {item['role']}" if action == "show" else item["path"]
                lines.append("  " + inline_text(label))
                if action == "memories" and item.get("digest"):
                    lines.append("    digest: " + inline_text(item["digest"]))
                lines.extend("    " + inline_text(line) for line in item["content"].splitlines())
        if data.get("next_offset") is not None:
            tokens = ["vbot", "external", action, args.source]
            if action == "show":
                tokens.append(args.session)
            for flag, value in (
                ("--path", args.path),
                ("--query", getattr(args, "query", None)),
                ("--limit", args.limit),
                ("--offset", data["next_offset"]),
            ):
                if value is not None:
                    tokens.extend([flag, str(value)])
            if action in {"show", "memories"}:
                tokens.extend(["--char-offset", str(data.get("next_char_offset", 0))])
            lines.append("Next: " + format_command((*tokens, *target_options(instance))))
    if action == "import-session":
        lines.append(
            "Next: "
            + format_command(
                ("vbot", "session", "show", data["session_id"], *target_options(instance))
            )
        )
        lines.append(
            "Undo: "
            + format_command(
                (
                    "vbot",
                    "session",
                    "delete",
                    data["agent_id"],
                    data["session_id"],
                    *target_options(instance),
                )
            )
        )
    if action == "import-memory":
        lines.append("Original saved: " + inline_text(data["saved_source_path"]))
    lines.extend("Note: " + inline_text(warning) for warning in data.get("warnings", []))
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)
