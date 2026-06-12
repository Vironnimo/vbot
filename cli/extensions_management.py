"""Extension visibility and enable/disable RPC commands for the vBot CLI.

`list` reads `extensions.list`; `enable`/`disable` are thin wrappers over
`settings.update` (full-replace `extensions` section). Enable/disable are
restart-applied (extensions are never hot-reloaded), so the output points at
`vbot server restart`.
"""

from __future__ import annotations

from collections.abc import Sequence
from difflib import get_close_matches
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

RESTART_HINT = "restart required: run 'vbot server restart' to apply"


def extensions_list(instance: ServerInstance) -> CommandResult:
    """Return formatted extension catalog output from `extensions.list` RPC."""

    extensions = _load_extensions(instance)
    if isinstance(extensions, CommandResult):
        return extensions
    return CommandResult(ok=True, message=_format_extension_rows(extensions), instance=instance)


def extensions_enable(instance: ServerInstance, name: str) -> CommandResult:
    """Remove *name* from the disabled set via `settings.update` (restart-applied)."""

    return _set_disabled(instance, name, disable=False)


def extensions_disable(instance: ServerInstance, name: str) -> CommandResult:
    """Add *name* to the disabled set via `settings.update` (restart-applied)."""

    return _set_disabled(instance, name, disable=True)


def _set_disabled(instance: ServerInstance, name: str, *, disable: bool) -> CommandResult:
    extensions = _load_extensions(instance)
    if isinstance(extensions, CommandResult):
        return extensions

    known_names = [
        ext["name"]
        for ext in extensions
        if isinstance(ext, dict) and isinstance(ext.get("name"), str)
    ]
    if name not in known_names:
        return CommandResult(
            ok=False, message=_format_unknown_extension(name, known_names), instance=instance
        )

    currently_disabled = [
        ext["name"] for ext in extensions if isinstance(ext, dict) and ext.get("disabled")
    ]
    if disable and name in currently_disabled:
        return CommandResult(
            ok=True,
            message=f"extension '{name}' is already disabled (no change)",
            instance=instance,
        )
    if not disable and name not in currently_disabled:
        return CommandResult(
            ok=True, message=f"extension '{name}' is already enabled (no change)", instance=instance
        )

    if disable:
        disabled = [*currently_disabled, name]
    else:
        disabled = [other for other in currently_disabled if other != name]

    config = {
        ext["name"]: ext["config"]
        for ext in extensions
        if isinstance(ext, dict) and isinstance(ext.get("config"), dict) and ext["config"]
    }

    update = _rpc_call(
        instance, "settings.update", {"extensions": {"disabled": disabled, "config": config}}
    )
    if not update.ok:
        return update.to_command_result()

    action = "disabled" if disable else "enabled"
    lines = [f"extension '{name}' {action}"]
    if update.data.get("restart_required"):
        lines.append(RESTART_HINT)
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def _load_extensions(instance: ServerInstance) -> list[Any] | CommandResult:
    payload = _rpc_call(instance, "extensions.list", {})
    if not payload.ok:
        return payload.to_command_result()
    extensions = payload.data.get("extensions")
    if not isinstance(extensions, list):
        return CommandResult(
            ok=False, message="RPC result missing extensions list", instance=instance
        )
    return extensions


def _format_extension_rows(extensions: Sequence[object]) -> str:
    if not extensions:
        return "no extensions discovered"

    lines = ["extensions:"]
    for extension in extensions:
        lines.extend(_format_extension_row(extension))
    return "\n".join(lines)


def _format_extension_row(extension: object) -> list[str]:
    if not isinstance(extension, dict):
        return ["- invalid extension entry"]

    name = _string_or_default(extension.get("name"), "?")
    status = _string_or_default(extension.get("status"), "?")
    header = f"- {name}  {status}"
    version = extension.get("version")
    if isinstance(version, str) and version:
        header += f"  v{version}"
    description = extension.get("description")
    if isinstance(description, str) and description:
        header += f"  {description}"

    rows = [header]
    error = extension.get("error")
    if isinstance(error, str) and error:
        rows.append(f"    error: {error}")
    capabilities = _format_capabilities(extension.get("capabilities"))
    if capabilities:
        rows.append(f"    {capabilities}")
    capability_errors = extension.get("capability_errors")
    if isinstance(capability_errors, list):
        for capability_error in capability_errors:
            if isinstance(capability_error, str) and capability_error:
                rows.append(f"    warning: {capability_error}")
    return rows


def _format_capabilities(capabilities: object) -> str:
    if not isinstance(capabilities, dict):
        return ""

    parts: list[str] = []
    hooks = capabilities.get("hooks")
    if isinstance(hooks, dict) and hooks:
        hook_summary = ", ".join(f"{event}({count})" for event, count in hooks.items())
        parts.append(f"hooks: {hook_summary}")
    tools = capabilities.get("tools")
    if isinstance(tools, list) and tools:
        parts.append(f"tools: {', '.join(str(tool) for tool in tools)}")
    backends = capabilities.get("recall_backends")
    if isinstance(backends, list) and backends:
        parts.append(f"recall_backends: {', '.join(str(backend) for backend in backends)}")
    if capabilities.get("startup"):
        parts.append("startup")
    if capabilities.get("shutdown"):
        parts.append("shutdown")
    return "; ".join(parts)


def _format_unknown_extension(name: str, candidates: list[str]) -> str:
    lines = [f"extension '{name}' not found"]
    if candidates:
        lines.append(f"available extensions: {', '.join(candidates)}")
        suggestions = get_close_matches(name, candidates, n=1)
        if suggestions:
            lines.append(f"did you mean: {suggestions[0]}")
    return "\n".join(lines)


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
