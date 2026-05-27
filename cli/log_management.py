"""Log viewer RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from cli.server_management import CommandResult, ServerInstance

RPC_PATH = "/api/rpc"
RPC_TIMEOUT_SECONDS = 10.0


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


def log_read(instance: ServerInstance, file_name: str) -> CommandResult:
    """Read one log file via `log.read` RPC."""

    payload = _rpc_call(instance, "log.read", {"file": file_name})
    if not payload.ok:
        return payload.to_command_result()
    entries = payload.data.get("entries")
    if not isinstance(entries, list):
        return CommandResult(ok=False, message="RPC result missing log entries", instance=instance)
    resolved_file = _string_or_default(payload.data.get("file"), file_name)
    cursor = _string_or_default(payload.data.get("cursor"), "-")
    return CommandResult(
        ok=True,
        message=_format_log_entries(resolved_file, entries, cursor),
        instance=instance,
    )


class _RpcPayload:
    def __init__(
        self,
        *,
        ok: bool,
        instance: ServerInstance,
        data: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> None:
        self.ok = ok
        self.instance = instance
        self.data = data or {}
        self.message = message

    def to_command_result(self) -> CommandResult:
        return CommandResult(ok=False, message=self.message, instance=self.instance)


def _rpc_call(instance: ServerInstance, method: str, params: dict[str, Any]) -> _RpcPayload:
    request_body = {"method": method, "params": params}
    try:
        response = httpx.post(
            f"{instance.url}{RPC_PATH}",
            json=request_body,
            timeout=RPC_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as exc:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=f"RPC request failed: {exc.__class__.__name__}",
        )

    try:
        payload = response.json()
    except ValueError:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=f"RPC response was not JSON (HTTP {response.status_code})",
        )

    if not isinstance(payload, dict):
        return _RpcPayload(ok=False, instance=instance, message="RPC response must be an object")

    if response.status_code != httpx.codes.OK:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=_rpc_error_message(
                payload.get("error"),
                fallback=f"RPC request failed with HTTP {response.status_code}",
            ),
        )

    ok_flag = payload.get("ok")
    if ok_flag is True:
        result = payload.get("result", {})
        if not isinstance(result, dict):
            return _RpcPayload(ok=False, instance=instance, message="RPC result must be an object")
        return _RpcPayload(ok=True, instance=instance, data=result)
    if ok_flag is False:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=_rpc_error_message(payload.get("error"), fallback="RPC request failed"),
        )

    return _RpcPayload(ok=False, instance=instance, message="RPC response missing boolean ok flag")


def _rpc_error_message(error: object, *, fallback: str) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        if isinstance(code, str) and isinstance(message, str):
            return f"{code}: {message}"
        if isinstance(message, str):
            return message
    return fallback


def _format_log_files(files: Sequence[object], default_file: object) -> str:
    if not files:
        return "no log files"

    default_text = _string_or_default(default_file, "-")
    lines = [f"logs: default={default_text}"]
    for file_name in files:
        lines.append(f"- {_string_or_default(file_name, '?')}")
    return "\n".join(lines)


def _format_log_entries(file_name: str, entries: Sequence[object], cursor: str) -> str:
    lines = [f"log: {file_name}", f"cursor: {cursor}"]
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


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
