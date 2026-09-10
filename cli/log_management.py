"""Log viewer RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Sequence

from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def log_list(instance: ServerInstance) -> CommandResult:
    """Return available log files via `log.list` RPC."""

    payload = _rpc_call(instance, "log.list", {})
    if not payload.ok:
        return payload.to_command_result()
    files = payload.data.get("files")
    if not isinstance(files, list):
        return CommandResult(ok=False, message="RPC result missing files list", instance=instance)
    return CommandResult(
        ok=True,
        message=_format_log_files(files, payload.data.get("default_file")),
        instance=instance,
    )


def log_read(
    instance: ServerInstance, file_name: str, *, limit: int = 100, level: str | None = None
) -> CommandResult:
    """Read one log file via `log.read` RPC."""

    payload = _rpc_call(instance, "log.read", {"file": file_name})
    if not payload.ok:
        return payload.to_command_result()
    entries = payload.data.get("entries")
    if not isinstance(entries, list):
        return CommandResult(ok=False, message="RPC result missing log entries", instance=instance)
    resolved_file = _string_or_default(payload.data.get("file"), file_name)
    if level is not None:
        entries = [
            entry for entry in entries if isinstance(entry, dict) and entry.get("level") == level
        ]
    total = len(entries)
    selected = entries[-limit:] if limit > 0 else entries
    return CommandResult(
        ok=True,
        message=_format_log_entries(
            resolved_file,
            selected,
            f"showing {len(selected)} of {total} matching entries; --limit 0 reads all",
        ),
        instance=instance,
    )


def _format_log_files(files: Sequence[object], default_file: object) -> str:
    if not files:
        return "no log files"

    default_text = _string_or_default(default_file, "-")
    lines = [f"logs: default={default_text}"]
    for file_name in files:
        lines.append(f"- {_string_or_default(file_name, '?')}")
    return "\n".join(lines)


def _format_log_entries(file_name: str, entries: Sequence[object], cursor: str) -> str:
    lines = [f"log: {file_name}", cursor]
    if not entries:
        lines.append("no entries")
        return "\n".join(lines)

    for entry in entries:
        lines.append(_format_log_entry(entry))
    return "\n".join(lines)


def _format_log_entry(entry: object) -> str:
    if not isinstance(entry, dict):
        return "- invalid log entry"

    timestamp = _string_or_default(entry.get("timestamp"), "?")
    level = _string_or_default(entry.get("level"), "?")
    logger_name = _string_or_default(entry.get("logger_name"), "?")
    message = _string_or_default(entry.get("message"), "")
    line = f"- {timestamp} [{level}] {logger_name} - {message}"
    continuation = _string_or_default(entry.get("continuation"), "")
    if continuation:
        return f"{line}\n  {continuation}"
    return line
